#!/bin/bash
# Build DeSyDe at the TODAES tag, for the RQ1 head-to-head (desyde_cmp.py).
#
#   tag v0.2.1-todaes = commit 6d5cbebb7b2294717f6381e1bb08896d75da8066
#   https://github.com/forsyde/DeSyDe/tree/v0.2.1-todaes
#
# Needs: Gecode 4.4.0 with Gist in /usr/local (the version of the TODAES
# article), libxml2, boost (filesystem, program_options). The MiniZinc snap's
# Gecode 6.4.0 is a FlatZinc solver only (no headers, no libraries) and
# cannot be linked against.
#
# desyde-build.patch changes only what a current compiler and boost need:
# three casts of '\0' to char*, a missing 'auto', two #includes, and no
# -lboost_system (header-only since boost 1.69). No change to the CP model.
#
# Gecode's Gist needs libQt5PrintSupport.so.5, which is not installed here;
# it is fetched as a .deb, unpacked into deps/ (no root), and found through
# DT_RPATH (RUNPATH would not reach libgecodegist's own dependencies).
set -euo pipefail
cd "$(dirname "$0")"
TAG=v0.2.1-todaes
SHA=6d5cbebb7b2294717f6381e1bb08896d75da8066

if [ ! -d build ]; then
  git clone -q --branch "$TAG" https://github.com/forsyde/DeSyDe.git build
fi
test "$(git -C build rev-parse HEAD)" = "$SHA"
git -C build diff --quiet && git -C build apply ../desyde-build.patch

if [ ! -e deps/root/usr/lib/x86_64-linux-gnu/libQt5PrintSupport.so.5 ]; then
  mkdir -p deps
  (cd deps && apt-get download libqt5printsupport5t64 &&
   dpkg -x libqt5printsupport5t64_*.deb root)
fi
D=$(realpath deps/root/usr/lib/x86_64-linux-gnu)
mkdir -p logs
make -C build -j4 \
  LDFLAGS="-Wl,--disable-new-dtags -Wl,-rpath-link,$D -Wl,-rpath,$D" \
  > logs/build.log 2>&1
build/bin/adse --version
