#!/bin/bash -e
PYTHONPATH="$PYTHONPATH:$(pwd)/lintian-brush:$(pwd)/upstream-ontologist:$(pwd)/silver-platter:$(pwd)/breezy:$(pwd)/python-debian/lib:$(pwd)/ognibuild" python3 -m janitor.runner "$@"
