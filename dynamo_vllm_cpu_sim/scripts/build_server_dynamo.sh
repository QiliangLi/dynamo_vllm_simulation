#!/usr/bin/env bash
# Build the server's Dynamo 1.2 source in an isolated Linux directory.
set -euo pipefail

source_dir=$(realpath "${1:?provide Dynamo 1.2 source directory}")
build_dir=$(realpath -m "${2:?provide build output directory}")
python=$(command -v "${3:-python3}")
case "$build_dir/" in
    "$source_dir/"*) echo "Build directory must be outside the source tree" >&2; exit 2 ;;
esac
mkdir -p "$build_dir/source"
cp -a "$source_dir/." "$build_dir/source/"

export PYO3_PYTHON="$python"
export CARGO_TARGET_DIR="$build_dir/target"
export RUSTFLAGS="${RUSTFLAGS:---cfg tokio_unstable -C target-cpu=generic}"
export CARGO_PROFILE_DEV_DEBUG=0 CARGO_PROFILE_DEV_OPT_LEVEL=2
cd "$build_dir/source/lib/bindings/python"
cargo build --locked --features tfs-admission -j "${CARGO_BUILD_JOBS:-4}" > "$build_dir/build.log" 2>&1
mkdir -p "$build_dir/python/dynamo"
cp -a src/dynamo/. "$build_dir/python/dynamo/"
cp "$CARGO_TARGET_DIR/debug/lib_core.so" "$build_dir/python/dynamo/_core.abi3.so"
PYTHONPATH="$build_dir/python:$build_dir/source/components/src" "$python" -m dynamo.frontend --help > "$build_dir/frontend.help"
printf '%s\n' "$build_dir/python"
