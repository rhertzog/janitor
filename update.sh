#!/bin/sh -x
# TODO(jelmer): Create a Debian package for this
if [ ! -d lintian-brush ]; then
    brz branch https://salsa.debian.org/jelmer/lintian-brush
else
    brz pull -d lintian-brush
fi
if [ ! -d silver-platter ]; then
    brz branch https://jelmer.uk/bzr/silver-platter/trunk silver-platter
else
    brz pull -d silver-platter
fi
